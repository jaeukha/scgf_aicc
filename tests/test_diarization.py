from aicc.diarization import StableSpeakerLabels
from types import SimpleNamespace
import sys

import numpy as np

from model_server import WhisperXRuntime, diarize_jobs


def test_whisperx_runtime_returns_existing_asr_contract(monkeypatch):
    calls = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            calls.update(kwargs)
            return iter([SimpleNamespace(text=" 첫 문장 "), SimpleNamespace(text=" 둘째 문장 ")]), None

    class FakePipeline:
        model = FakeModel()

    monkeypatch.setitem(sys.modules, "whisperx", SimpleNamespace(load_model=lambda *args, **kwargs: FakePipeline()))
    text, _ = WhisperXRuntime("cpu").transcribe(np.zeros(16000), "한국어", None)
    assert text == "첫 문장 둘째 문장"
    assert calls == {"language": "ko", "beam_size": 5, "vad_filter": False}


def test_rolling_diarization_keeps_speaker_number_when_model_relabels():
    labels = StableSpeakerLabels()
    first = labels.label([{"start": 0, "end": 2, "speaker": "SPEAKER_00"}], 0, 2)
    second = labels.label([
        {"start": 0, "end": 2, "speaker": "SPEAKER_07"},
        {"start": 2, "end": 4, "speaker": "SPEAKER_03"},
    ], 2, 4)
    assert first == "speaker_1"
    assert second == "speaker_2"


def test_long_file_diarization_is_sent_in_ordered_windows():
    calls = []

    class FakeDiarizer:
        def turns(self, session_id, samples):
            calls.append((session_id, len(samples)))
            return [{"start": 0, "end": len(samples) / 16000, "speaker": f"speaker_{len(calls)}"}]

    jobs = [SimpleNamespace(start=1, end=2, samples=np.zeros(16000)), SimpleNamespace(start=16, end=17, samples=np.zeros(16000))]
    result = diarize_jobs(FakeDiarizer(), "session", np.zeros(32 * 16000, dtype=np.float32), jobs)
    assert [speaker for _, speaker in result] == ["speaker_1", "speaker_2"]
    assert [call[1] for call in calls] == [15 * 16000, 15 * 16000]


def test_diarization_splits_one_asr_job_at_speaker_turns():
    class FakeDiarizer:
        def turns(self, session_id, samples):
            return [{"start": 0, "end": 1, "speaker": "speaker_1"},
                    {"start": 1, "end": 2, "speaker": "speaker_2"}]

    job = SimpleNamespace(start=0, end=2, samples=np.zeros(32000))
    result = diarize_jobs(FakeDiarizer(), "session", np.zeros(32000, dtype=np.float32), [job])
    assert [(round(part.start), round(part.end), speaker, len(part.samples)) for part, speaker in result] == [
        (0, 1, "speaker_1", 16000), (1, 2, "speaker_2", 16000)]
