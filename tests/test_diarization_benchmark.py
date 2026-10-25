from diarization_benchmark_app import result_rows


def test_result_rows_keeps_diarization_alignment():
    turns = [{"start": 0.0, "end": 1.2, "speaker": "SPEAKER_00"}, {"start": 1.2, "end": 2.5, "speaker": "SPEAKER_01"}]
    assert result_rows(turns, ["첫 발화", "둘째 발화"], ["one", "two"]) == [
        ["0.00–1.20", "SPEAKER_00", "첫 발화", "one"],
        ["1.20–2.50", "SPEAKER_01", "둘째 발화", "two"],
    ]
