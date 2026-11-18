"""Independently decodable, bounded mono Opus clips for TRACE."""
import ctypes as c
from functools import lru_cache
import logging
from pathlib import Path
import platform
import struct

import numpy as np

from trace_engine.speech_activity import RATE


logger = logging.getLogger(__name__)
TAG = b'TCA2'
MAX_SAMPLES = 13 * RATE
_FRAME = RATE // 50  # 20 ms, mono.
_HEADER = struct.Struct('<4sIHH')  # Tag, samples, encoder delay, packet bytes.
_BITRATE = 4002
_VBR = 4006
_COMPLEXITY = 4010
_LOOKAHEAD = 4027
_APPLICATIONS = {'voip': 2048, 'audio': 2049, 'lowdelay': 2051}


class OpusCodec:
    """Encode independently decodable 16 kHz mono clips using a libopus binary."""

    def __init__(self, library, bitrate=32000, application='voip'):
        if not 6000 <= bitrate <= 64000 or bitrate % 400:
            raise ValueError('TRACE Opus bitrate must use whole 20 ms packet sizes')
        self.bitrate = bitrate
        self._application = _APPLICATIONS[application]
        self._lib = c.CDLL(str(Path(library).resolve(strict=True)))
        pointer, integer = c.c_void_p, c.c_int
        declarations = {
            'opus_encoder_create': (pointer, (integer, integer, integer, c.POINTER(integer))),
            'opus_encoder_ctl': (integer, (pointer, integer)),
            'opus_encoder_destroy': (None, (pointer,)),
            'opus_encode': (integer, (pointer, pointer, integer, pointer, integer)),
            'opus_decoder_create': (pointer, (integer, integer, c.POINTER(integer))),
            'opus_decoder_destroy': (None, (pointer,)),
            'opus_decode': (integer, (pointer, pointer, integer, pointer, integer, integer)),
        }
        for name, (result, arguments) in declarations.items():
            function = getattr(self._lib, name)
            function.restype, function.argtypes = result, arguments

    def encode(self, pcm):
        pcm = np.ascontiguousarray(pcm, dtype='<i2')
        if pcm.ndim != 1 or not 0 < len(pcm) <= MAX_SAMPLES:
            raise ValueError('Invalid Opus clip length')
        error, lookahead = c.c_int(), c.c_int()
        encoder = self._lib.opus_encoder_create(RATE, 1, self._application, c.byref(error))
        if not encoder or error.value:
            raise ValueError(f'Opus encoder error {error.value}')
        try:
            ctl = self._lib.opus_encoder_ctl
            for setting, value in ((_BITRATE, self.bitrate), (_VBR, 0), (_COMPLEXITY, 5)):
                if ctl(encoder, setting, c.c_int(value)):
                    raise ValueError('Invalid Opus encoder setting')
            if ctl(encoder, _LOOKAHEAD, c.byref(lookahead)) or not 0 <= lookahead.value <= _FRAME:
                raise ValueError('Invalid Opus encoder delay')
            frames = (len(pcm) + lookahead.value + _FRAME - 1) // _FRAME
            padded = np.zeros(frames * _FRAME, dtype='<i2')
            padded[:len(pcm)] = pcm
            packet_size = self.bitrate // 400
            output = c.create_string_buffer(frames * packet_size)
            for frame in range(frames):
                count = self._lib.opus_encode(encoder, padded.ctypes.data + frame * _FRAME * 2,
                    _FRAME, c.byref(output, frame * packet_size), packet_size)
                if count != packet_size:
                    raise ValueError(f'Unexpected Opus packet size {count}')
            return _HEADER.pack(TAG, len(pcm), lookahead.value, packet_size) + output.raw
        finally:
            self._lib.opus_encoder_destroy(encoder)

    def decode(self, data):
        if len(data) < _HEADER.size:
            raise ValueError('Incomplete Opus clip header')
        tag, length, lookahead, packet_size = _HEADER.unpack_from(data)
        if tag != TAG or not 0 < length <= MAX_SAMPLES or lookahead > _FRAME or not 15 <= packet_size <= 160:
            raise ValueError('Invalid Opus clip header')
        frames = (length + lookahead + _FRAME - 1) // _FRAME
        if len(data) != _HEADER.size + frames * packet_size:
            raise ValueError('Invalid Opus clip packet data')
        error = c.c_int()
        decoder = self._lib.opus_decoder_create(RATE, 1, c.byref(error))
        if not decoder or error.value:
            raise ValueError(f'Opus decoder error {error.value}')
        try:
            payload = c.create_string_buffer(data[_HEADER.size:])
            pcm = np.empty(frames * _FRAME, dtype='<i2')
            for frame in range(frames):
                count = self._lib.opus_decode(decoder, c.byref(payload, frame * packet_size), packet_size,
                    pcm.ctypes.data + frame * _FRAME * 2, _FRAME, 0)
                if count != _FRAME:
                    raise ValueError(f'Unexpected Opus frame size {count}')
            return pcm[lookahead:lookahead + length]
        finally:
            self._lib.opus_decoder_destroy(decoder)


@lru_cache(maxsize=1)
def bundled_opus():
    """Load the packaged Windows x64 codec when compatible with this process."""
    if platform.system() != 'Windows' or platform.machine().casefold() not in {'amd64', 'x86_64'} or c.sizeof(c.c_void_p) != 8:
        return None
    library = Path(__file__).parent / 'assets' / 'windows_x64' / 'opus.dll'
    try:
        return OpusCodec(library, bitrate=32000, application='audio')
    except (OSError, AttributeError):
        logger.warning('Bundled TRACE Opus codec unavailable; using compressed PCM for new clips')
        return None
