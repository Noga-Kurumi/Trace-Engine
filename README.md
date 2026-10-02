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
