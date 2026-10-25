"""화자 분리 모델의 임시 라벨을 통화 중 안정적인 화자 번호로 바꾼다."""
from dataclasses import dataclass, field


@dataclass
class StableSpeakerLabels:
    """겹치는 롤링 윈도우에서 pyannote의 SPEAKER_XX 재번호화를 흡수한다."""
    max_speakers: int = 2
    history: list[tuple[float, float, str]] = field(default_factory=list)
    next_id: int = 1

    @staticmethod
    def _overlap(left, right):
        return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))

    def stabilize(self, segments):
        """현재 롤링 윈도우의 모델 라벨을 통화 전체에서 쓸 라벨로 바꾼다."""
        old = self.history[:]
        by_model = {}
        for segment in segments:
            by_model.setdefault(segment["speaker"], []).append((segment["start"], segment["end"]))
        mapping, claimed = {}, set()
        for model_label, turns in by_model.items():
            scores = {}
            for turn in turns:
                for start, end, stable in old:
                    scores[stable] = scores.get(stable, 0.0) + self._overlap(turn, (start, end))
            ranked = sorted(scores, key=scores.get, reverse=True)
            stable = next((item for item in ranked if item not in claimed and scores[item] > 0), None)
            if stable is None and len({item[2] for item in old} | set(mapping.values())) < self.max_speakers:
                stable = f"speaker_{self.next_id}"
                self.next_id += 1
            if stable:
                mapping[model_label] = stable
                claimed.add(stable)
        if not segments:
            return []
        window_start = min(segment["start"] for segment in segments)
        window_end = max(segment["end"] for segment in segments)
        self.history = [turn for turn in old if turn[1] <= window_start or turn[0] >= window_end]
        self.history.extend((segment["start"], segment["end"], mapping.get(segment["speaker"], "unknown"))
                            for segment in segments)
        return [{**segment, "speaker": mapping.get(segment["speaker"], "unknown")} for segment in segments]

    def label(self, segments, current_start, current_end):
        """하위 호환: 현재 구간과 가장 많이 겹치는 안정화 화자 하나를 반환한다."""
        stable = self.stabilize(segments)
        current = max(stable, key=lambda s: self._overlap((s["start"], s["end"]), (current_start, current_end)),
                      default=None)
        return current["speaker"] if current else "unknown"
