# Hermes Business Powerpack

**Beta Telegram Business plugin for Hermes Agent: invoke a full agent in a private conversation, transcribe voice messages, analyze reply attachments, and manage your Telegram stories.**

Powerpack waits for the account owner's explicit text or voice invocation, runs the native Hermes CLI with tools, and delivers its final text directly into that Business conversation. It also provides voice transcription, reply-attachment analysis and owner-controlled Telegram stories.

**The recipient sees the response, sent on behalf of your personal account.** There is no Send/Edit/Discard approval screen. Enable this only if that workflow is what you want.

[Russian documentation](README.ru.md)

## Features

- Invoke Hermes with a message beginning with `Hermes` or `Гермес`. Example: `Hermes, summarize the document I am replying to.`
- Voice invocation: the same strict prefix must appear at the start of the transcription. Sender identity is checked using Telegram IDs and the actual connection owner, not voice recognition.
- Voice messages without an owner invocation are transcribed. A conversation partner's voice never invokes the tool agent, even if it contains the wake word.
- Same-conversation replies provide quoted text/captions and temporary attachments: photos, documents, audio, voice, video, video notes and animations. File download is bounded to 20 MiB; actual format readability depends on Hermes tools.
- Native Hermes CLI sessions are separated by profile, bot, Business connection and peer chat. The owner's main Hermes session is not mixed in.
- Exact verified routing is stored before agent execution, so recipient identity survives failures and restarts. This does not authorize retries or future sends.
- In the owner's ordinary private chat with the bot, `/story` publishes an attached photo/video or media from a reply. The `business_story` tool can publish, edit, list registered IDs, or delete stories created by this plugin.

## Requirements and compatibility

- Linux: this implementation uses `fcntl` locks and process-group cleanup.
- A Telegram account that supports Business/Chat Automation connections, with the bot enabled for Business Mode in BotFather.
- A recent Hermes Agent build providing `ctx.register_telegram_handler`, `ctx.register_tool`, native one-shot stream-JSON CLI sessions, profile-local secret scopes and `served_profile_child_env`.
- python-telegram-bot in the declared 22.x range; tested with 22.6. Pillow, and FFmpeg/FFprobe with libx265 for video stories.
- Configured Hermes speech-to-text for voice transcription; suitable vision/video/document tools for attachment analysis.

This beta has been tested on its development Hermes installation, including native plugin loading and dispatch in isolated test profiles. Compatibility with every stock Hermes release is **not** claimed. The integration uses Hermes Python interfaces that can evolve; an older build missing them is not supported.

## Installation

From a compatible Hermes installation:

```sh
hermes plugins install https://github.com/web3blind/hermes-business-powerpack --enable
```

For reproducible installs, pass `--ref` with a reviewed full commit SHA. Configure the owner's numeric Telegram ID through Hermes' configuration interface before allowing Business traffic. The example below describes the resulting configuration shape, not credentials:

```yaml
plugins:
  entries:
    business-powerpack:
      owner_ids: [111111]
      triggers: [Hermes, Гермес]
      timeout: 300
      stt_timeout: 180
      max_concurrent: 2
      max_voice_bytes: 10485760
      max_voice_seconds: 300
      cli: hermes
```

`111111` is a placeholder. An empty owner list disables processing. Set `owner_ids` with `hermes config set plugins.entries.business-powerpack.owner_ids '[YOUR_NUMERIC_TELEGRAM_ID]'`, replacing the placeholder with your ID.

Restart the gateway after installation or upgrades (`hermes gateway restart`). Existing callbacks may retain objects from the previous plugin version; a hot-reload acknowledgment is not proof that all changes are live.

In Telegram, connect this bot through Business/Chat Automation settings, choose the conversations it may receive, and grant reply permission if you want responses. Optionally configure `allowed_chats` as a second, narrower allowlist; an explicitly empty list denies all chats. This does not grant access to the full account history.

Do not enable Powerpack and another handler of Business messages on the same bot without reviewing handler ordering: Powerpack consumes Business updates before ordinary handlers.

## Stories

Grant `can_manage_stories` separately from reply permission. Send a photo/video to the bot in your ordinary owner DM with `/story Optional caption`, or reply to media with `/story`. A command with usable media requests immediate publication; without media it shows instructions.

The default duration is 24 hours. The tool supports 6/12/24/48 hours and only edits/deletes stories recorded as its own. Listing registered IDs is not a read-back of what viewers currently see. Public Bot API does not provide arbitrary story read-back. Financial operations, gifts and Stars are not implemented.

## Security and failure behavior

- Connection ownership and current reply rights are checked before work and each outbound text chunk. Revocation cancels active tasks. There is no ordinary bot-DM fallback.
- Conversation text, peer labels and attachments are untrusted data, not tool authorization. Attachments are not intentionally executed as scripts or macros.
- The full-tool agent operates within the same Hermes profile. This is **not a filesystem sandbox**; do not ask it to disclose private profile data to a conversation partner.
- Inbound messages are durably claimed before work. Duplicate updates are not rerun. An unknown tool or send outcome is never retried automatically after failure or restart.
- A verified route mapping stores bot, connection, chat and owner IDs plus a bounded peer label in the profile's private SQLite database. Stored routing is not stored permission. Legacy hash-only records cannot be reversed or guessed.
- Temporary reply attachments are removed after processing, errors and cancellation. An abrupt process kill can leave temporary files. Prepared story media is retained privately for subsequent edits.
- Busy routes and exhausted concurrency skip new requests; there is no durable work queue. Errors are recorded privately and may result in no conversation response. Reply-attachment download failures return a safe explanation.
- Only final successful CLI text is delivered, not intermediate reasoning or tool logs. Generated media is not delivered through the Business text-response path.
- Native Hermes tool approval rules remain in force; an interactive approval request can fail or time out in headless mode.

## Data

Plugin state lives under the bound profile's `business-powerpack/` directory. Directory permissions are 0700 and the SQLite database is 0600. Native Hermes separately stores each Business CLI session's history. Story records include captions and prepared-media paths. Never publish profile databases, logs, tokens, downloaded media or private session transcripts.

## Testing

Run from a Hermes checkout with the plugin dependencies installed:

```sh
scripts/run_tests.sh /absolute/path/to/hermes-business-powerpack/tests -q
```

Tests use synthetic Telegram updates, isolated Hermes homes and controlled transports/subprocesses. They do not send live Telegram messages or publish test stories. The development verification additionally exercised a native Hermes tool call and reply-image analysis; this is not certification of every file format or deployment.

## License and credits

MIT. Independent implementation on Hermes plugin surfaces. Workflow comparison and credits: [hermes-telegram-business](https://github.com/NousResearch/hermes-telegram-business) and [hermes-powerpack-sborka](https://github.com/Human20app/hermes-powerpack-sborka). No private profile state or local development Git history is included in this public repository.
