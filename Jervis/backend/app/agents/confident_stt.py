"""Whisper STT that exposes per-utterance ASR confidence.

pipecat's TranscriptionFrame carries a ``result`` field that the default
service leaves empty. We fill it with faster-whisper's ``avg_logprob`` so the
conversation manager can gate repair requests on real transcription quality.
"""

import asyncio

import numpy as np
from loguru import logger

from pipecat.frames.frames import ErrorFrame, TranscriptionFrame
from pipecat.services.settings import assert_given
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.utils.time import time_now_iso8601


class ConfidenceWhisperSTTService(WhisperSTTService):
    async def run_stt(self, audio: bytes):
        if not self._model:
            yield ErrorFrame("Whisper model not available")
            return

        await self.start_processing_metrics()

        audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        language = assert_given(self._settings.language)
        segments, _ = await asyncio.to_thread(
            self._model.transcribe, audio_float, language=language
        )

        text = ""
        avg_logprob = None
        no_speech_prob = None
        no_speech_prob_threshold = assert_given(self._settings.no_speech_prob)

        for segment in segments:
            if (
                no_speech_prob_threshold is not None
                and segment.no_speech_prob < no_speech_prob_threshold
            ):
                text += f"{segment.text} "
                avg_logprob = (
                    segment.avg_logprob
                    if avg_logprob is None
                    else min(avg_logprob, segment.avg_logprob)
                )
                no_speech_prob = (
                    segment.no_speech_prob if no_speech_prob is None else no_speech_prob
                )

        await self.stop_processing_metrics()

        if text:
            await self._handle_transcription(text, True, language)
            logger.debug(f"Transcription: [{text}] conf={avg_logprob}")
            frame = TranscriptionFrame(
                text,
                self._user_id,
                time_now_iso8601(),
                language,
            )
            frame.result = {
                "avg_logprob": float(avg_logprob) if avg_logprob is not None else None,
                "no_speech_prob": float(no_speech_prob) if no_speech_prob is not None else None,
            }
            yield frame
