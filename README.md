# TRACE

**Temporal Retrieval & Activity Context Engine**

TRACE captures local Windows activity, persists it as text events in SQLite, and provides retrieval via keywords, time ranges, and MCP (Model Context Protocol).

## Installation

For development, install it as an editable dependency:

```powershell
pip install -e .

```

TRACE can be consumed by any local application needing system activity context, including AI assistants.

The TRACE core is cross-platform: SQLite storage, the timeline, and search logic do not require OS-specific APIs. Native Windows collectors are optional:

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

On Linux and macOS, TRACE operates as a persistence and retrieval engine. Platform-specific collectors can be provided by the host application or through future plugins.

## Custom Transcriber Injection

To avoid duplicating transcription models in memory, an application that already maintains a transcriber can inject it directly:

```python
trace = TraceEngine(transcriber=existing_transcriber)

```

The injected object must expose a `transcribe(audio)` method. The meeting collector will reuse this instance instead of spinning up an extra Whisper model.

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

Run regression and existing storage/MCP smoke checks with:

```powershell
python -m unittest discover -s tests -v
```


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

## Source validation and snapshot distribution

Run tests against this checkout rather than an installed snapshot:

```powershell
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -v
```

Lindsay Desktop consumes a hashed wheel built from this source tree. Its
`dependencies/trace-lock.json` records the source commit, individual file hashes
and wheel digest; updating the source checkout alone does not update a consumer.
TRACE remains licensed under AGPL-3.0; see [LICENSE](LICENSE).
