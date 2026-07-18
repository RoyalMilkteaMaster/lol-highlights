"""多維度 watchdog。

判斷錄影是否健康：
1. process alive（streamlink + ffmpeg 都還在跑）
2. **目錄內所有 .ts 累積總大小**持續成長
3. 超過 no_growth_timeout 沒成長 → 視為僵死

restart_count 由 recorder 主流程管理，watchdog 只負責「判斷健康」。

bug fix：
舊版用「最新一個 .ts 的 size」比較，每次 ffmpeg 切到新 90 秒 segment 時，
新 .ts 從 0 開始寫，size 永遠不會超過上一段的 size（例：part_002=22MB → part_003 從 0），
導致 watchdog 誤判「沒成長」180 秒後 fail。

實測 LCK：streamlink + ffmpeg 都正常工作，11 段 .ts 全部正確產生
（每 90 秒一段、總大小 290MB），但 watchdog 在第一次 segment 切換後就誤判 fail。

修法：改看「sum of all .ts file sizes」。任何一段在寫都算成長，
新建 .ts 也算（總大小一定增加）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class Watchdog:
    """錄影健康度追蹤器。

    Args:
        ts_dir              : .ts 段落輸出目錄
        prefix              : .ts 檔名前綴（用來 glob `<prefix>_part_*.ts`）
        no_growth_timeout   : 多少秒沒成長視為僵死（默認 180s）
    """

    def __init__(
        self,
        ts_dir: Path,
        prefix: str,
        *,
        no_growth_timeout: float = 180.0,
    ) -> None:
        self.ts_dir = ts_dir
        self.prefix = prefix
        self.no_growth_timeout = no_growth_timeout
        self._last_total_size = 0
        self._last_growth_at = time.time()
        self._diagnosis = ""

    def healthy(self, sl_proc, ff_proc) -> bool:
        """檢查是否仍在正常錄影。"""
        # 1. 兩個 process 都活著
        if sl_proc.poll() is not None:
            self._diagnosis = f"streamlink 進程結束 (rc={sl_proc.returncode})"
            return False
        if ff_proc.poll() is not None:
            self._diagnosis = f"ffmpeg 進程結束 (rc={ff_proc.returncode})"
            return False

        # 2. 看「目錄內所有 .ts 累積總大小」是否成長
        # （取代舊版「最新 .ts size」邏輯，避免 segment 切換時誤判）
        total_size = self._total_size()

        if total_size > self._last_total_size:
            self._last_total_size = total_size
            self._last_growth_at = time.time()
            return True

        if time.time() - self._last_growth_at > self.no_growth_timeout:
            latest = self._latest_ts()
            self._diagnosis = (
                f"目錄總大小 {total_size} bytes 超過 "
                f"{self.no_growth_timeout}s 沒成長"
                + (f"（最新 .ts={latest.name}）" if latest else "（尚無 .ts 段）")
            )
            return False
        return True

    def diagnosis(self) -> str:
        """回傳上一次失敗原因（給 error_message 用）。"""
        return self._diagnosis or "unknown"

    def _total_size(self) -> int:
        """目錄內所有 .ts 段的累積總大小（bytes）。"""
        total = 0
        for p in self.ts_dir.glob(f"{self.prefix}_part_*.ts"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _latest_ts(self) -> Path | None:
        files = sorted(self.ts_dir.glob(f"{self.prefix}_part_*.ts"))
        return files[-1] if files else None
