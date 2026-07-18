"""精華片段資料結構（Segment dataclass）。

Segment 由 pipeline/clip.py 產生，rendering/video_editor.py 消費，
代表最終要剪入精華影片的單一片段，含起訖、評分、標籤、轉場與靜音旗標。
"""

from dataclasses import dataclass, field


@dataclass
class Segment:
    start: float        # 影片秒數
    end: float          # 影片秒數
    score: int
    labels: list[str] = field(default_factory=list)
    game_start: float = 0.0   # 對應的遊戲秒數（debug 用）
    transition_before: bool = True   # False = 與前一段無縫拼接（同窗口子片段）
    mute_original_audio: bool = False  # True = 該段強制原音靜音（如結算圖表只留 BGM）
