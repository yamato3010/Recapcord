import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import discord
from discord.voice import VoiceClient
from dotenv import load_dotenv

from audio import AudioClip, RecordingSink, save_transcript
from yachiyo_lines import yachiyo_line


log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
RECORDING_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}_\d+_[0-9a-f]{8}", re.ASCII)


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
    message = yachiyo_line("transcript_saved", count=count)
    attachment = discord.File(path)
    try:
        await send(message, file=attachment)
    finally:
        attachment.close()


@dataclass
class Recording:
    voice: VoiceClient
    sink: RecordingSink
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

    pending = ROOT / "transcripts" / ".pending"
    processing: set[Path] = set()

    def transcribe(path, manifest):
        nonlocal model
        directory = pending / path.stem
        clips = [
            AudioClip(clip["speaker_id"], clip["start"], directory / clip["file"])
            for clip in manifest["clips"]
        ]
        if clips and model is None:
            from faster_whisper import WhisperModel

            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        count = save_transcript(
            model, clips, {int(key): name for key, name in manifest["speaker_names"].items()},
            language, path, started_at=datetime.fromisoformat(manifest["started_at"]),
        )
        try:
            shutil.rmtree(directory)
        except OSError:
            log.exception("Could not remove saved recording audio %s", directory)
        return count

    async def process(path, manifest):
        processing.add(path)
        try:
            async with inference_lock:
                return await asyncio.to_thread(transcribe, path, manifest)
        finally:
            processing.discard(path)

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

            guild = bot.get_guild(guild_id)
            if guild is not None:
                await resolve_speaker_names(
                    guild,
                    {clip.speaker_id for clip in recording.sink.clips},
                    recording.sink.speaker_names,
                )
            manifest = {
                "guild_id": guild_id,
                "owner_id": recording.owner_id,
                "started_at": recording.started_at.isoformat(),
                "speaker_names": recording.sink.speaker_names,
                "clips": [
                    {"speaker_id": clip.speaker_id, "start": clip.start, "file": clip.path.name}
                    for clip in recording.sink.clips
                ],
            }
            staging = recording.sink.directory / "recording.tmp"
            staging.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            staging.replace(staging.with_suffix(".json"))
            return await process(recording.path, manifest)
        finally:
            sessions.pop(guild_id, None)

    async def report_failure(send, path):
        log.exception("文字起こしに失敗しました: %s", path.stem)
        message = yachiyo_line("transcription_failed")
        if (pending / path.stem / "recording.json").is_file():
            message += f" 音声は保存してあるよ。再試行は `/recap_retry recording_id:{path.stem}` でできるよ〜。"
        try:
            await send(message)
        except discord.HTTPException:
            log.warning("Discord could not report failure for %s", path.stem, exc_info=True)

    async def deliver(ctx, path, count):
        log.info("Saved %s (%s segments)", path, count)
        try:
            await send_transcript(ctx.followup.send, path, count)
        except discord.HTTPException:
            try:
                await send_transcript(ctx.channel.send, path, count)
            except discord.HTTPException:
                log.warning("Discord could not deliver transcript %s", path, exc_info=True)

    @bot.event
    async def on_ready():
        log.info("Logged in as %s", bot.user)

    @bot.slash_command(description="参加中のボイスチャンネルで録音を開始", contexts={discord.InteractionContextType.guild})
    async def recap_start(ctx: discord.ApplicationContext):
        guild_id = ctx.guild_id
        state = getattr(ctx.author, "voice", None)
        channel = state.channel if state else None
        if not isinstance(channel, discord.VoiceChannel):
            await ctx.respond(yachiyo_line("join_voice"))
            return
        if guild_id in sessions or guild_id in connecting:
            await ctx.respond(yachiyo_line("recording_busy"))
            return

        connecting.add(guild_id)
        directory = None
        voice = None
        try:
            await ctx.defer()
            voice = await channel.connect(timeout=20)
            display_name = discord.utils.escape_markdown(ctx.author.display_name)
            await channel.send(
                yachiyo_line("recording_started", display_name=display_name),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            started_at = datetime.now().astimezone()
            stamp = started_at.strftime("%Y-%m-%d_%H%M%S")
            path = ROOT / "transcripts" / f"{stamp}_{guild_id}_{uuid4().hex[:8]}.md"
            (pending / path.stem).mkdir(mode=0o700, parents=True)
            directory = pending / path.stem
            sink = RecordingSink(directory)
            voice.start_recording(sink)
            sessions[guild_id] = Recording(
                voice, sink, channel.id, ctx.author.id, started_at, path
            )
        except Exception:
            log.exception("Could not start recording in guild %s", guild_id)
            try:
                if voice is not None:
                    await voice.disconnect(force=True)
            except Exception:
                log.exception("Voice disconnect failed in guild %s", guild_id)
            finally:
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
            await ctx.respond(yachiyo_line("recording_start_failed"))
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
            await ctx.respond(yachiyo_line("no_recording"))
            return
        if ctx.author.id != recording.owner_id and not ctx.author.guild_permissions.manage_channels:
            await ctx.respond(yachiyo_line("stop_forbidden"))
            return

        recording.stopping = True
        try:
            await ctx.respond(yachiyo_line("transcribing"))
        finally:
            try:
                count = await finish(guild_id, recording)
            except Exception:
                await report_failure(ctx.channel.send, recording.path)
            else:
                await deliver(ctx, recording.path, count)

    @bot.slash_command(description="保存済みの録音から文字起こしを再試行", contexts={discord.InteractionContextType.guild})
    async def recap_retry(ctx: discord.ApplicationContext, recording_id: str):
        missing = "このサーバーで再試行できる録音が見つからなかったよ〜。"
        if not RECORDING_ID.fullmatch(recording_id):
            await ctx.respond(missing, ephemeral=True)
            return

        path = pending.parent / f"{recording_id}.md"
        try:
            manifest = json.loads((pending / recording_id / "recording.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            await ctx.respond(missing, ephemeral=True)
            return
        except (OSError, ValueError):
            log.exception("Could not read saved recording %s", recording_id)
            await ctx.respond(yachiyo_line("transcription_failed"), ephemeral=True)
            return

        if manifest["guild_id"] != ctx.guild_id:
            await ctx.respond(missing, ephemeral=True)
            return
        if ctx.author.id != manifest["owner_id"] and not ctx.author.guild_permissions.manage_channels:
            await ctx.respond("再試行できるのは、録音を始めた人か「チャンネルの管理」権限を持ってる人だよ〜。", ephemeral=True)
            return
        if path.is_file():
            await ctx.respond("その録音は文字起こし済みだよ〜。", ephemeral=True)
            return
        if path in processing:
            await ctx.respond("その録音は今、文字起こし中だよ〜。", ephemeral=True)
            return

        processing.add(path)
        try:
            await ctx.respond("保存した音声から、もう一度文字起こしするよ〜。")
            count = await process(path, manifest)
        except Exception:
            await report_failure(ctx.channel.send, path)
        else:
            await deliver(ctx, path, count)
        finally:
            processing.discard(path)

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
            await report_failure(before.channel.send, recording.path)
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
