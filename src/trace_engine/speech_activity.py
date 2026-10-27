"""Sample-based endpointing. No text inspection, word stitching or intent rules."""
from collections import deque
from dataclasses import dataclass
import numpy as np

RATE=16000


@dataclass(frozen=True)
class SpeechChunk:
    audio: np.ndarray
    started_at: float
    ended_at: float
    overlap_seconds: float=0


class SpeechSegmenter:
    def __init__(self,detector,*,max_seconds=8.0,silence_seconds=.32,pre_seconds=.16,overlap_seconds=.16):
        self.detector=detector
        self.maximum=max(16000,min(12*RATE,int(max_seconds*RATE)))
        self.silence=max(512,int(silence_seconds*RATE))
        # Prefer a nearby natural pause over cutting a final syllable at the
        # nominal maximum. Still enforce a strict thirteen-second PCM budget.
        self.hard_maximum=min(13*RATE-512,self.maximum+max(int(.8*RATE),self.silence+int(.5*RATE)))
        self.pre=deque(maxlen=max(1,int(pre_seconds*RATE)//512))
        self.overlap=max(0,min(3200,int(overlap_seconds*RATE)))
        self.pending=np.empty(0,dtype=np.float32)
        self.pending_start=0.;self.parts=[];self.samples=0;self.quiet=0
        self.start=0.;self.copied_context=0
        self.new_speech=False

    def feed(self,audio,started_at):
        audio=np.ascontiguousarray(audio,dtype=np.float32)
        if audio.ndim!=1 or len(audio)>RATE*2 or not np.isfinite(audio).all():raise ValueError('Invalid capture packet')
        if not len(self.pending):self.pending_start=started_at
        self.pending=np.concatenate((self.pending,audio))
        result=[]
        while len(self.pending)>=512:
            frame=self.pending[:512].copy();self.pending=self.pending[512:]
            begin=self.pending_start;self.pending_start+=512/RATE
            result.extend(self._frame(frame,begin))
        return result

    def _frame(self,frame,started_at):
        analysis=np.pad(frame,(0,512-len(frame))) if len(frame)<512 else frame
        speech=self.detector(analysis)>=.5
        if speech:self.new_speech=True
        if not self.parts:
            self.pre.append((frame,started_at))
            if not speech:return []
            self.parts=[p for p,_ in self.pre];self.start=self.pre[0][1]
            self.samples=sum(len(p) for p in self.parts);self.pre.clear()
        else:
            self.parts.append(frame);self.samples+=len(frame)
        self.quiet=0 if speech else self.quiet+len(frame)
        if self.quiet>=self.silence:
            if self.new_speech:return [self._flush(False)]
            self.parts=[];self.samples=0;self.copied_context=0;self.quiet=0
        if self.samples>=self.hard_maximum:return [self._flush(True)]
        return []

    def _flush(self,forced):
        audio=np.concatenate(self.parts);audio.setflags(write=False)
        result=SpeechChunk(audio,self.start,self.start+len(audio)/RATE,self.copied_context/RATE)
        if forced and self.overlap:
            tail=audio[-self.overlap:].copy()
            self.parts=[tail];self.samples=len(tail);self.start=result.ended_at-len(tail)/RATE
            self.copied_context=len(tail)
        else:
            self.parts=[];self.samples=0;self.copied_context=0
        self.quiet=0
        self.new_speech=False
        return result

    def finish(self):
        result=[]
        if len(self.pending):
            pending,self.pending=self.pending,np.empty(0,dtype=np.float32)
            result.extend(self._frame(pending,self.pending_start))
        if self.parts and self.new_speech:result.append(self._flush(False))
        self.parts=[];self.samples=0;self.pre.clear();self.copied_context=0
        return result
