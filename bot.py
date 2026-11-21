import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import discord
from discord.voice import VoiceClient
from dotenv import load_dotenv

from audio import RecordingSink, save_transcript


log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


async def resolve_speaker_names(guild, speaker_ids, names):
    for speaker_id in speaker_ids:
        member = guild.get_member(speaker_id)
        if member is None and speaker_id not in names:
            try:
                member = await guild.fetch_member(speaker_id)
            except discord.HTTPException:
                log.warning("Could not resolve display name for %s in guild %s", speaker_id, guild.id)
        if member is not None:
            names[speaker_id] = member.display_name


async def send_transcript(send, path, count):
    message = (
        f"めでたしや〜！文字起こしをバッチリPCに保存したよ〜（全部で{count}件！）。"
        "これで大事なことも忘れずにすむね〜。"
    )
    attachment = discord.File(path)
    try:
        await send(message, file=attachment)
    finally:
        attachment.close()


@dataclass
class Recording:
    voice: VoiceClient
    sink: RecordingSink
    scratch: tempfile.TemporaryDirectory
    channel_id: int
    owner_id: int
    started_at: datetime
    path: Path
    stopping: bool = False


def create_bot(guild_id: int | None, model_name: str, language: str | None):
    intents = discord.Intents.default()
    options = {"debug_guilds": [guild_id]} if guild_id else {}
    bot = discord.Bot(intents=intents, **options)
    sessions: dict[int, Recording] = {}
    connecting: set[int] = set()
    model = None
    inference_lock = asyncio.Lock()

    def transcribe(recording):
        nonlocal model
        if recording.sink.clips and model is None:
            from faster_whisper import WhisperModel

            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        return save_transcript(
            model, recording.sink.clips, recording.sink.speaker_names, language, recording.path,
            started_at=recording.started_at,
        )

    async def finish(guild_id, recording):
        try:
            try:
                if recording.voice.is_recording():
                    recording.voice.stop_recording()
            finally:
                recording.sink.cleanup()
                try:
                    await recording.voice.disconnect(force=True)
                except Exception:
                    log.exception("Voice disconnect failed in guild %s", guild_id)

            if not recording.sink.clips:
                return transcribe(recording)
            guild = bot.get_guild(guild_id)
            if guild is not None:
                await resolve_speaker_names(
                    guild,
                    {clip.speaker_id for clip in recording.sink.clips},
                    recording.sink.speaker_names,
                )
            async with inference_lock:
                return await asyncio.to_thread(transcribe, recording)
        finally:
            recording.scratch.cleanup()
            sessions.pop(guild_id, None)

    @bot.event
    async def on_ready():
        log.info("Logged in as %s", bot.user)

    @bot.slash_command(description="参加中のボイスチャンネルで録音を開始", contexts={discord.InteractionContextType.guild})
    async def recap_start(ctx: discord.ApplicationContext):
        guild_id = ctx.guild_id
        state = getattr(ctx.author, "voice", None)
        channel = state.channel if state else None
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.respond("先にボイスチャンネルに入ってから、ヤッチョを呼んでね〜！")
            return
        if guild_id in sessions or guild_id in connecting:
            await ctx.respond(
                "おっと、このサーバーでは録音の準備か、録音か、文字起こしの途中だよ〜。"
                "そっちが終わってから呼んでね！",
            )
            return

        connecting.add(guild_id)
        scratch = None
        voice = None
        try:
            await ctx.defer()
            scratch = tempfile.TemporaryDirectory(prefix="recapcord-")
            voice = await channel.connect(timeout=20)
            display_name = discord.utils.escape_markdown(ctx.author.display_name)
            await channel.send(
                f"うけたまかしこまつかまつり〜！{display_name}の録音を開始するよ〜！"
                "音声はヤッチョがいるPCでしっかり文字起こししておくね。"
                "終わるときは /recap_stop って呼んでね〜！",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            started_at = datetime.now().astimezone()
            stamp = started_at.strftime("%Y-%m-%d_%H%M%S")
            path = ROOT / "transcripts" / f"{stamp}_{guild_id}_{uuid4().hex[:8]}.md"
            sink = RecordingSink(Path(scratch.name))
            voice.start_recording(sink)
            sessions[guild_id] = Recording(
                voice, sink, scratch, channel.id, ctx.author.id, started_at, path
            )
        except Exception:
            log.exception("Could not start recording in guild %s", guild_id)
            try:
                if voice is not None:
                    await voice.disconnect(force=True)
            except Exception:
                log.exception("Voice disconnect failed in guild %s", guild_id)
            finally:
                if scratch is not None:
                    scratch.cleanup()
            await ctx.respond(
                "ごみん、録音を始められなかったよ〜。ヤッチョの権限と起動ログを見てみてね。",
            )
        else:
            try:
                await ctx.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not clear the deferred start reply in guild %s", guild_id)
        finally:
            connecting.discard(guild_id)

    @bot.slash_command(description="録音を止めて文字起こしファイルを受け取る", contexts={discord.InteractionContextType.guild})
    async def recap_stop(ctx: discord.ApplicationContext):
        guild_id = ctx.guild_id
        recording = sessions.get(guild_id)
        if recording is None or recording.stopping:
            await ctx.respond("おっと、今は止められる録音がないみたいだよ〜。")
            return
        if ctx.author.id != recording.owner_id and not ctx.author.guild_permissions.manage_channels:
            await ctx.respond(
                "録音を止められるのは、録音を始めた人か「チャンネルの管理」権限を持ってる人だよ〜。",
            )
            return

        recording.stopping = True
        try:
            await ctx.respond(
                "は〜い、録音をストップしたよ〜！いと大儀〜。今一生懸命まとめてるから、"
                "保存先は処理が終わり次第ここへのお返事と起動ログでお知らせするね〜。",
            )
        finally:
            try:
                count = await finish(guild_id, recording)
            except Exception:
                log.exception("文字起こしに失敗しました")
                try:
                    await ctx.followup.send(
                        "ごめんねぇ〜。文字起こしがうまくいかなかったの。起動ログを見てみてね。",
                    )
                except discord.HTTPException:
                    pass
            else:
                log.info("Saved %s (%s segments)", recording.path, count)
                try:
                    await send_transcript(ctx.followup.send, recording.path, count)
                except discord.HTTPException:
                    # Long transcriptions can outlive the interaction token.
                    try:
                        await send_transcript(ctx.channel.send, recording.path, count)
                    except discord.HTTPException:
                        log.warning("Discord could not deliver transcript %s", recording.path, exc_info=True)

    @bot.event
    async def on_voice_state_update(member, before, after):
        if member.id != bot.user.id or before.channel is None or before.channel == after.channel:
            return
        recording = sessions.get(member.guild.id)
        if recording is None or recording.stopping or before.channel.id != recording.channel_id:
            return

        recording.stopping = True
        try:
            count = await finish(member.guild.id, recording)
            log.info("Saved %s (%s segments) after leaving the voice channel", recording.path, count)
        except Exception:
            log.exception("文字起こしに失敗しました")
        else:
            try:
                await send_transcript(before.channel.send, recording.path, count)
            except discord.HTTPException:
                log.warning("Discord could not deliver transcript %s", recording.path, exc_info=True)

    return bot


def main():
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(".env に DISCORD_TOKEN を設定してください。")

    opus_library = os.getenv("OPUS_LIBRARY")
    if opus_library:
        discord.opus.load_opus(opus_library)
    else:
        # Loading a decoder also checks whether Pycord can locate libopus.
        try:
            discord.opus.Decoder()
        except discord.opus.OpusNotLoaded as exc:
            raise SystemExit("libopus をインストールし、必要に応じて OPUS_LIBRARY を設定してください。") from exc

    logging.basicConfig(level=logging.INFO)
    guild = os.getenv("DISCORD_GUILD_ID")
    bot = create_bot(
        int(guild) if guild else None,
        os.getenv("WHISPER_MODEL", "small"),
        os.getenv("WHISPER_LANGUAGE", "ja") or None,
    )
    bot.run(token)


if __name__ == "__main__":
    main()
