# TRACE

**Temporal Retrieval & Activity Context Engine**

TRACE records desktop activity in a local SQLite timeline and retrieves it by
time range, source or search term. Applications can call its Python API or use
MCP (Model Context Protocol). Windows hosts can enable collectors for foreground
windows, clipboard, screen text and meeting audio.

## Installation

TRACE requires Python 3.11 or newer. From a source checkout, install it as an
editable dependency:

```powershell
pip install -e .

```

SQLite storage, timeline queries and search run across platforms. Install the
Windows extras for native collection:

```powershell
pip install -e ".[windows]"

```

## Basic Usage

```python
from trace_engine import TraceEngine

with TraceEngine() as trace:
    trace.insert("clipboard", "copied text")
    results = trace.search("text")

```

On Linux and macOS, host applications can insert observations from their own
collectors and query the timeline through the same API.

## Custom Transcriber Injection

To avoid duplicating transcription models in memory, an application that already maintains a transcriber can inject it directly:

```python
trace = TraceEngine(transcriber=existing_transcriber)

```

For direct requests, implement `transcribe(audio, source=..., priority=...)`.
For live meeting capture, also implement `submit(audio, source=..., priority=...)`
and return a future that supports `get(timeout=...)`. The engine calls
`close()` during shutdown.

## Lifecycle and screen context

`pause_collectors()` pauses capture while keeping shared transcription and
storage available. `stop()` closes all resources, even when capture was never
started, and returns whether shutdown succeeded. A closed engine cannot be
restarted. Injected transcribers must also implement `close()`; an injected
transcriber prevents creation of an unused second Whisper service.

CPU and CLI transcription support cancellation during shutdown. Requests queued
at shutdown, or submitted afterward, receive an error result instead of waiting
indefinitely. Streaming pipe writes are serialized separately from lifecycle
operations so shutdown can terminate a process blocked on input.

When live UIA and OCR are unavailable, screen context may use an OCR record from
the preceding hour. Historical content includes its timestamp and recorded
window, and explicitly does not confirm the current screen. Clipboard, audio
and window-change events are never presented as OCR. Temporary database
connections used by this fallback are closed.

## Bounded historical evidence

`trace_engine.retrieval.read_context` reads recorded evidence without starting
collectors or inference. Time, source and excluded-application filters apply
before the result limit. Responses include event IDs, timestamps, recorded
windows, bounded excerpts and continuation cursors; historical observations
never assert the current state of the screen.

```python
from trace_engine.retrieval import read_context

context = read_context(
    "timeline.db", time_window="yesterday", query="Project notes",
    sources=["ocr", "clipboard"], limit=3, max_bytes=2000,
)
```

`read_session_context` in `trace_engine.session_context` produces on-demand
extractive session capsules with deduplicated excerpts. It uses SQLite and
recorded session membership rather than another language model. Original event
IDs and character offsets remain available for inspecting the complete evidence.
Document/task links in `trace_engine.work_context` describe observed access and
explicit filesystem operations; a link does not establish a document's contents.
Queries have bounded output sizes and database execution deadlines.

## Meeting context and host integration

Meeting capture accepts a host-provided process-audio backend, speech detector,
optional anonymous speaker segmenter and shared transcription service. This lets
the desktop host choose its installed STT engine without loading a second model.
Capture sessions retain immutable source, process and time provenance, so a late
transcription is attached to its original session even after another call starts.

Speech buffering, queues and shutdown are bounded. Session state records gaps,
incomplete transcripts and capture errors instead of presenting missing speech as
a complete meeting. Browsers require explicit opt-in and capture scope covers the
browser process, not a verified individual tab. Speech detection does not promise
music removal; anonymous speaker labels do not establish personal identity.
Application-isolated capture and its supported call applications depend on the
host's Windows backend and consent settings.

Screen, clipboard and window collectors record observation provenance, and the
host can supply capture exclusion/privacy callbacks. The engine does not bypass
the application's privacy policy, mutate documents or dispatch assistant tools.

## Encrypted media history preview

These interfaces are validated in the Lindsay Desktop TRACE wheel
`0.1.0.dev0+snapshot.01fd292b9c39`. That wheel contains the encrypted-media
implementation. An install from the upstream source branch currently provides
the timeline, text retrieval and live meeting APIs documented above.

Use `trace_engine.visual_archive.VisualArchive` with an existing timeline
database. `record(session_id, captured_at, jpeg)` ties an image to a foreground
session. Use Unix timestamps in seconds. `select(start, end, point=...)` finds
one nearby capture for a point or up to three across a range. `read(snapshot)`
returns the JPEG. The archive caps disk usage and supports
retention via `prune(cutoff)`. The Windows
`trace_engine.collectors.visual_history.VisualWindowCollector` tracks focus,
takes periodic screenshots only for focused windows, and deduplicates
unchanged frames. It accepts the host's grabber, OCR reader, exclusion filters
and capture eligibility rules. The grabber receives a capture job, mode, screen
handle and exclusion callback, plus the keyword `window_only`.

`trace_engine.call_archive.CallArchive` stores bounded clips and cached
transcripts. Its `select` searches past clips, including keyword matches from
its keyed index. `trace_engine.call_index.CallIndex` prioritizes old clips for
background indexing and coalesces lookups for a clip already being transcribed.
The host provides `service()` and `admit(service, background)` callbacks to
choose its current ASR service and enforce its own resource policy. The service
accepts `submit(audio, source=..., priority=...)` and returns a future with
`get_nowait()`.

```python
from trace_engine.call_archive import CallArchive
from trace_engine.call_index import CallIndex
from trace_engine.collectors.meeting_audio import MeetingAudioCollector

archive = CallArchive(timeline.db_path)
archive.initialize()
collector = MeetingAudioCollector(timeline, config=meeting_config, segment_sink=archive.record)
index = CallIndex(archive, service=current_asr, admit=admit_asr)
```

Start and stop the collector and index alongside the host's capture lifecycle.
Prune the archives on the host's retention schedule. A Windows host can use
`TraceEngine(collector_factories=...)` to replace `window`, `clipboard`,
`screen` or `meeting` collectors. Each factory receives `db` and the keyword
argument `config`. Use `None` to suppress a collector. Existing configuration
flags still control whether optional collectors are enabled.

By default both archives use Windows DPAPI for the current user. Other hosts
can supply `seal` and `decrypt` callables. Call archives require NumPy and use
the packaged libopus on Windows x64. Other hosts can pass
`opus=lambda: OpusCodec(library_path)` with their own libopus. New clips use
deflate-compressed PCM when the codec is unavailable. Stored Opus clips need
a compatible decoder.
Existing DPAPI images, Opus clips, and older compressed PCM remain readable
with the same Windows profile and a compatible codec. The preview wheel
includes the libopus license and build manifest in
`trace_engine/assets/windows_x64`.

## Source validation and snapshot distribution

Run tests against this checkout on Windows. The MCP smoke test also exercises
the local stdio server.

```powershell
pip install -e ".[windows,mcp]"
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
```

Lindsay Desktop consumes a hashed wheel built from this source tree. Its
`dependencies/trace-lock.json` records the source commit, individual file hashes
and wheel digest so consumers can verify the exact code they use.

## License

TRACE is available under the [AGPL-3.0](LICENSE), which permits commercial use
subject to its terms. The copyright holder grants Lindsay Desktop separate
permission to incorporate owner-controlled TRACE code under proprietary terms.
Third-party components retain their own licenses.
