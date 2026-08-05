"""Workaround for livekit SDK 1.1.13 AudioStream stereo->mono downmix bug.

The SDK's ``rtc.AudioStream(track)`` defaults to ``num_channels=1``. When the
remote publisher sends 2-channel audio (e.g. browser mic), the SDK's internal
downmix produces digital silence. Requesting ``num_channels=2`` yields real
audio, so we subscribe with 2 channels and downmix to mono ourselves before the
(mono-only) pipecat resampler.
"""

import array

from livekit import rtc
from pipecat.frames.frames import AudioRawFrame
from pipecat.transports.livekit.transport import (
    LiveKitInputTransport,
    LiveKitTransportClient,
)


async def _patched_on_track_subscribed(self, track, publication, participant):
    if track.kind == rtc.TrackKind.KIND_AUDIO:
        await self._close_audio_stream(participant.sid)
        self._audio_tracks[participant.sid] = track
        audio_stream = rtc.AudioStream(track, num_channels=2)
        task = self._task_manager.create_task(
            self._process_audio_stream(audio_stream, participant.sid),
            f"{self}::_process_audio_stream",
        )
        self._audio_streams[participant.sid] = (audio_stream, task)
        await self._callbacks.on_audio_track_subscribed(participant.sid)
    elif track.kind == rtc.TrackKind.KIND_VIDEO:
        await self._close_video_stream(participant.sid)
        self._video_tracks[participant.sid] = track
        if self._params.video_in_enabled:
            video_stream = rtc.VideoStream(track)
            task = self._task_manager.create_task(
                self._process_video_stream(video_stream, participant.sid),
                f"{self}::_process_video_stream",
            )
            self._video_streams[participant.sid] = (video_stream, task)
        await self._callbacks.on_video_track_subscribed(participant.sid)


async def _patched_convert(self, audio_frame_event):
    audio_frame = audio_frame_event.frame
    raw = audio_frame.data.tobytes()
    nc = audio_frame.num_channels
    if nc > 1:
        samples = array.array("h", raw)
        mono = array.array("h")
        step = nc
        for i in range(0, len(samples) - step + 1, step):
            mono.append(sum(samples[i : i + step]) // step)
        raw = mono.tobytes()
        nc = 1
    audio_data = await self._resampler.resample(
        raw, audio_frame.sample_rate, self.sample_rate
    )
    return AudioRawFrame(
        audio=audio_data,
        sample_rate=self.sample_rate,
        num_channels=nc,
    )


def apply_livekit_audio_patch():
    LiveKitTransportClient._async_on_track_subscribed = _patched_on_track_subscribed
    LiveKitInputTransport._convert_livekit_audio_to_pipecat = _patched_convert
