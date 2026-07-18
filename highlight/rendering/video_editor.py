"""
Video Editor — 使用 FFmpeg 剪輯精華影片

流程：
  1. 防禦性 Encode：統一縮放至 1080p，防止不同解析度造成 concat 失敗
  2. 依 segments 切出各片段
  3. 選擇性轉場合併：
       - transition_before=False（同場戰鬥：直播+Replay）→ seamless concat
       - transition_before=True（跨時空：不同戰場）→ xfade fadeblack 0.5s
  4. 音訊側鏈壓縮（Audio Ducking）：賽評說話時音樂自動降音量
  5. 若有 BP 片頭，用 xfade 拼接（取代原 7s 黑色過場）
"""

import json
import logging
import os
import random
import subprocess
import shutil
from pathlib import Path

from highlight.selection.segments import Segment

logger = logging.getLogger(__name__)

XFADE_DURATION = 0.5    # 跨時空轉場時長（秒）— fadeblack
TARGET_WIDTH   = 1920   # 防禦性縮放目標解析度
TARGET_HEIGHT  = 1080


def _detect_nvenc() -> bool:
    """偵測系統是否支援 h264_nvenc（Nvidia GPU 硬體編碼）。"""
    import yaml
    # 5/15：FFMPEG_BIN env var 優先（新機 .env 用），config.yaml fallback（舊機）
    _ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip()
    if not _ffmpeg_bin:
        try:
            _cfg_path = Path(__file__).parent.parent / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _cfg = yaml.safe_load(_f)
                _ffmpeg_bin = _cfg.get("ffmpeg_path", "")
        except Exception:
            pass
    if _ffmpeg_bin:
        os.environ["PATH"] = _ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "nullsrc",
             "-t", "0.1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# NVENC 偵測 — lazy，首次呼叫 _is_nvenc_available() 時才 spawn ffmpeg 探測
_NVENC_CACHE: bool | None = None


def _is_nvenc_available() -> bool:
    """Lazy 偵測：首次呼叫時跑 ffmpeg probe，之後讀 cache。"""
    global _NVENC_CACHE
    if _NVENC_CACHE is None:
        _NVENC_CACHE = _detect_nvenc()
        if _NVENC_CACHE:
            logger.info("h264_nvenc 可用，使用 GPU 硬體編碼")
        else:
            logger.info("h264_nvenc 不可用，使用 libx264 CPU 編碼")
    return _NVENC_CACHE


def _video_codec_args(preset: str = "medium", crf: int | str = 18) -> list[str]:
    """回傳適合當前系統的影片編碼參數。"""
    if _is_nvenc_available():
        # nvenc preset: p1(快)~p7(慢)，p4≈medium；cq=品質（類似 crf）
        nvenc_preset_map = {
            "ultrafast": "p1", "superfast": "p1", "veryfast": "p2",
            "faster": "p2", "fast": "p3", "medium": "p4",
            "slow": "p5", "slower": "p6", "veryslow": "p7",
        }
        np = nvenc_preset_map.get(str(preset), "p4")
        return ["-c:v", "h264_nvenc", "-preset", np, "-cq", str(crf)]
    else:
        return ["-c:v", "libx264", "-preset", str(preset), "-crf", str(crf)]


def _hwaccel_input(path) -> list[str]:
    """NVDEC 硬體解碼：NVENC 可用時加 -hwaccel cuda，否則純軟解。
    不加 -hwaccel_output_format，讓幀自動落到系統記憶體供 CPU filter 使用。
    """
    if _is_nvenc_available():
        return ["-hwaccel", "cuda", "-i", str(path)]
    return ["-i", str(path)]

# 結尾保護標籤：這些 segment 的 duration 不可被任何邏輯縮減
# 修 P：跟 clip_filter.py PROTECTED_LABELS 對齊（加 end_graph_visual、拿掉死字串 "victory"）
PROTECTED_LABELS = {"game_end", "victory_visual", "end_graph_visual"}


class VideoEditor:
    def __init__(self, config: dict):
        self.config = config
        self.hl_cfg  = config.get("highlight", {})
        self.out_cfg = config.get("output", {})
        self.music_cfg = config.get("music", {})
        # 5/15: FFMPEG_BIN env var 優先（新機 .env 用），config.yaml fallback（舊機）
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip() or config.get("ffmpeg_path", "")
        if ffmpeg_bin:
            os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")
        self._check_ffmpeg()

    def _check_ffmpeg(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("找不到 ffmpeg，請先安裝並確認在 PATH 中")

    # ── 公開入口 ──────────────────────────────────────────────────────────────

    def create_highlight(
        self,
        source_video: Path,
        segments: list[Segment],
        output_path: Path,
        music_file: Path | None = None,
        temp_dir: Path | None = None,
    ) -> Path:
        """
        主要入口：從 source_video 依 segments 剪輯並合併，輸出到 output_path。

        segments 的 transition_before 欄位：
          False → 與前一段無縫拼接（同場戰鬥：直播 → Replay）
          True  → 插入 xfade fadeblack 0.5s（跨時空：不同戰場）
        """
        if not segments:
            raise ValueError("沒有提供任何精華片段")

        if temp_dir is None:
            temp_dir = output_path.parent / f"_tmp_{output_path.stem}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        gameplay_music = music_file or self._pick_gameplay_music()

        try:
            # 1. 切片 + 防禦性 encode（統一 1080p）
            clip_paths = self._cut_clips(source_video, segments, temp_dir)

            # 2. 選擇性轉場合併
            trans_flags = [True] + [s.transition_before for s in segments[1:]]
            merged = self._xfade_concat(clip_paths, trans_flags, temp_dir / "merged.mp4")

            # 3. 音訊側鏈壓縮：賽評說話時音樂自動 duck
            highlight_with_audio = self._add_audio(
                merged, temp_dir / "highlight_audio.mp4", gameplay_music
            )

            # 4. BP 片頭：用 xfade 拼接（無 7s 黑色過場）
            bp_clip = self._make_bp_clip(source_video, temp_dir)
            if bp_clip:
                final = self._prepend_bp(bp_clip, highlight_with_audio, output_path)
            else:
                highlight_with_audio.rename(output_path)
                final = output_path

            return final

        finally:
            self._cleanup(temp_dir, output_path)

    # ── 步驟 1：切片 + 防禦性 Encode ──────────────────────────────────────────

    def _cut_clips(self, source: Path, segments: list[Segment], temp_dir: Path) -> list[Path]:
        clip_paths = []
        preset = self.out_cfg.get("ffmpeg_preset", "medium")
        crf    = self.out_cfg.get("ffmpeg_crf", 18)

        # 偵測來源解析度（判斷是否需要縮放）
        src_w, src_h = self._probe_resolution(source)
        needs_scale = (src_w != TARGET_WIDTH or src_h != TARGET_HEIGHT)
        if needs_scale:
            logger.info(f"來源解析度 {src_w}x{src_h}，縮放至 {TARGET_WIDTH}x{TARGET_HEIGHT}")

        for i, seg in enumerate(segments):
            raw     = temp_dir / f"raw_{i:03d}.mp4"
            encoded = temp_dir / f"clip_{i:03d}.mp4"

            # stream copy 快切
            self._run([
                "ffmpeg", "-y",
                "-ss", str(seg.start), "-to", str(seg.end),
                "-i", str(source),
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(raw),
            ], f"切片 {i+1}/{len(segments)}")

            # 防禦性縮放（統一 1080p）；Phase 42 起移除 drawtext debug label
            # Phase 45+ 修 M：找回 50ms afade-out（修 J 反向），讓 concat 群組內
            # 同戰場拼接不會 click pop；對 0.5s acrossfade 影響極小（只動末 50ms）。
            # 修 L：mute_original_audio=True 的段（end_graph_visual）整段原音設 0，
            # 後續 sidechaincompress ducking 因 orig=0 不觸發 → BGM 維持 full vol。
            seg_dur = self._get_duration(raw)
            if getattr(seg, "mute_original_audio", False):
                af = "volume=0"
            else:
                fadeout_st = max(0.0, seg_dur - 0.05)
                af = f"afade=t=in:st=0:d=0.05,afade=t=out:st={fadeout_st:.3f}:d=0.05"

            encode_cmd = ["ffmpeg", "-y", *_hwaccel_input(raw)]
            if needs_scale:
                scale_vf = (
                    f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}"
                    f":force_original_aspect_ratio=decrease,"
                    f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
                )
                encode_cmd += ["-vf", scale_vf]
            encode_cmd += [
                "-af", af,
                *_video_codec_args(preset, crf),
                "-c:a", "aac", "-b:a", "192k",
                str(encoded),
            ]
            self._run(encode_cmd, f"Encode 片段 {i+1}{'（縮放）' if needs_scale else ''}")

            raw.unlink(missing_ok=True)
            if not encoded.exists() or encoded.stat().st_size == 0:
                raise RuntimeError(f"Encode 失敗：{encoded.name} 未生成或為空檔（大小={encoded.stat().st_size if encoded.exists() else 'N/A'}）")
            logger.info(f"  [OK] {encoded.name}  {encoded.stat().st_size // 1024 // 1024} MB")
            clip_paths.append(encoded)

        return clip_paths

    # ── 步驟 2：選擇性轉場合併 ────────────────────────────────────────────────

    def _xfade_concat(
        self,
        clips: list[Path],
        transition_before: list[bool],
        output: Path,
    ) -> Path:
        """
        嚴格遵守 transition_before 標記：
          False（同場戰鬥，直播→Replay）→ concat 濾鏡無縫拼接，零轉場
          True（跨時空，不同戰場）→ xfade=fadeblack 0.5s 過渡

        流程：
          1. 依 transition_before=False 將連續片段分成群組（群組內無縫）
          2. 群組之間用 xfade fadeblack 合併
        """
        if len(clips) == 1:
            clips[0].rename(output)
            return output

        preset = self.out_cfg.get("ffmpeg_preset", "medium")
        crf    = str(self.out_cfg.get("ffmpeg_crf", 18))

        # ── Step 1：分群組（transition_before=True 為群組切割點）──────────────
        groups: list[list[Path]] = []
        cur_group: list[Path] = [clips[0]]
        for i in range(1, len(clips)):
            if transition_before[i]:
                groups.append(cur_group)
                cur_group = [clips[i]]
            else:
                cur_group.append(clips[i])
        groups.append(cur_group)

        # ── Step 2：群組內無縫拼接（同場戰鬥）────────────────────────────────
        group_files: list[Path] = []
        for g_idx, group in enumerate(groups):
            if len(group) == 1:
                group_files.append(group[0])
                continue

            group_out = output.parent / f"group_{g_idx:03d}.mp4"
            inputs = []
            for c in group:
                inputs += _hwaccel_input(c)
            fc = "".join(f"[{j}:v][{j}:a]" for j in range(len(group)))
            fc += f"concat=n={len(group)}:v=1:a=1[v][a]"
            self._run([
                "ffmpeg", "-y",
                *inputs,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "[a]",
                *_video_codec_args(preset, crf),
                "-c:a", "aac", "-b:a", "192k",
                str(group_out),
            ], f"無縫拼接群組 {g_idx}（{len(group)} 段，同場戰鬥）")
            group_files.append(group_out)

        # ── Step 3：群組之間 xfade fadeblack（跨時空）────────────────────────
        if len(group_files) == 1:
            group_files[0].rename(output)
            return output

        durations = [self._get_duration(c) for c in group_files]
        inputs_flat = []
        for c in group_files:
            inputs_flat += _hwaccel_input(c)

        fc_v, fc_a = [], []
        xd = XFADE_DURATION
        cumulative = 0.0
        prev_v, prev_a = "0:v", "0:a"

        for i in range(1, len(group_files)):
            cumulative += durations[i - 1] - xd
            ov, oa = f"v{i:02d}", f"a{i:02d}"
            fc_v.append(
                f"[{prev_v}][{i}:v]xfade=transition=fadeblack"
                f":duration={xd}:offset={cumulative:.3f}[{ov}]"
            )
            fc_a.append(
                f"[{prev_a}][{i}:a]acrossfade=d={xd}[{oa}]"
            )
            prev_v, prev_a = ov, oa

        filter_complex = ";".join(fc_v + fc_a)
        self._run([
            "ffmpeg", "-y",
            *inputs_flat,
            "-filter_complex", filter_complex,
            "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
            *_video_codec_args(preset, crf),
            "-c:a", "aac", "-b:a", "192k",
            str(output),
        ], f"xfade 合併 {len(group_files)} 個群組（跨時空）")

        return output

    # ── 步驟 3：音訊側鏈壓縮（Audio Ducking）─────────────────────────────────

    def _add_audio(self, merged: Path, output: Path, music_file: Path | None) -> Path:
        """
        加入背景音樂（3 段式，避免 GPU/CPU 搶資源）：
          Step 1  提取原聲音軌（AAC）
          Step 2  純 CPU 音訊混音（sidechaincompress ducking）
          Step 3  stream copy 合併影像 + 混音（近乎瞬間）

        若無音樂：直接調整原聲音量輸出。
        """
        music_vol = self.hl_cfg.get("music_volume", 0.15)
        orig_vol  = self.hl_cfg.get("original_volume", 1.0)
        # 5/12：loudnorm 響度標準化（user 反映 LCP 主播太小聲）
        # LCP source 平均 -29.2 dB / LCK -22.7 dB / LPL -17.0 dB → 拉到一致響度
        # I=-16 LUFS（YouTube 平台標準）/ LRA=11 LU / TP=-1.5 dB（防 clip）
        loudnorm_enabled = self.hl_cfg.get("loudnorm_enabled", True)
        loudnorm_i  = self.hl_cfg.get("loudnorm_i", -16)
        loudnorm_lra = self.hl_cfg.get("loudnorm_lra", 11)
        loudnorm_tp = self.hl_cfg.get("loudnorm_tp", -1.5)
        output.parent.mkdir(parents=True, exist_ok=True)

        if music_file:
            # 確保音樂路徑是絕對路徑
            if not Path(music_file).is_absolute():
                music_file = (Path(__file__).parent.parent / music_file).resolve()

        if music_file and Path(music_file).exists():
            logger.info(f"  BGM: {Path(music_file).name}  orig_vol={orig_vol}  music_vol={music_vol} "
                        f"loudnorm={loudnorm_enabled} (I={loudnorm_i})")

            src_audio   = output.parent / f"_srcaudio_{output.stem}.aac"
            mixed_audio = output.parent / f"_mixaudio_{output.stem}.aac"

            # Step 1: 提取原聲 + loudnorm 標準化響度（LCP 弱音會被拉強到接近 LCK/LPL 一致）
            step1_cmd = ["ffmpeg", "-y", "-i", str(merged), "-vn"]
            if loudnorm_enabled:
                step1_cmd += ["-af", f"loudnorm=I={loudnorm_i}:LRA={loudnorm_lra}:TP={loudnorm_tp}"]
            step1_cmd += ["-c:a", "aac", "-b:a", "192k", str(src_audio)]
            self._run(step1_cmd, "Step1 提取原聲音軌 + loudnorm 標準化")

            # Step 2: 純音訊 sidechaincompress（CPU-only，不與 GPU 搶資源）
            # Phase 29：ducking 收緊（threshold 0.12→0.08、ratio 3→4），讓 BGM 在主播講話時更安靜
            # threshold=0.08: 主播聲音稍高就壓；ratio=4: 更積極壓縮；level_sc=0.9: sidechain 更靈敏
            af = (
                f"[0:a]volume={orig_vol}[orig];"
                f"[1:a]volume={music_vol},aloop=loop=-1:size=2e+09[music_looped];"
                f"[music_looped][orig]sidechaincompress="
                f"threshold=0.08:ratio=4:attack=80:release=500:level_sc=0.9[music_ducked];"
                f"[orig][music_ducked]amix=inputs=2:duration=first:weights=1 1:normalize=0[aout]"
            )
            self._run([
                "ffmpeg", "-y",
                "-i", str(src_audio),
                "-stream_loop", "-1", "-i", str(music_file),
                "-filter_complex", af,
                "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                str(mixed_audio),
            ], "Step2 音訊混音（Audio Ducking，純 CPU）")

            # Step 3: 影像 + 混音 stream copy（瞬間完成）
            self._run([
                "ffmpeg", "-y",
                "-i", str(merged),
                "-i", str(mixed_audio),
                "-c:v", "copy", "-c:a", "copy",
                "-map", "0:v", "-map", "1:a",
                str(output),
            ], "Step3 合併影像與音軌（stream copy）")

            src_audio.unlink(missing_ok=True)
            mixed_audio.unlink(missing_ok=True)
        else:
            logger.warning(f"  無背景音樂（music_file={music_file}），只調整原聲音量"
                           f" loudnorm={loudnorm_enabled}")
            af = f"volume={orig_vol}"
            if loudnorm_enabled:
                af += f",loudnorm=I={loudnorm_i}:LRA={loudnorm_lra}:TP={loudnorm_tp}"
            self._run([
                "ffmpeg", "-y",
                "-i", str(merged),
                "-af", af,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                str(output),
            ], "無背景音樂，調整原聲音量 + loudnorm")

        return output

    # ── 步驟 4：BP 片頭（修正 E1）─────────────────────────────────────────────

    def _make_bp_clip(self, source: Path, temp_dir: Path) -> Path | None:
        """
        擷取 BP 片段（完全靜音，無原聲也無音樂）。
        音樂只從 game_start（bp_end）開始，由精華剪輯段負責。

        E1 防呆：
          - 若有 bp_start_time → 使用精確起始點
          - 若只有 bp_end_time 而無 bp_start_time → 自動往回推 75 秒
          - 若都沒有 bp_end_time → 不生成 BP 片頭
        """
        bp_duration = self.music_cfg.get("bp_duration", 0)
        if not bp_duration:
            return None

        # E1 防呆：自動計算 bp_start
        bp_end   = self.music_cfg.get("bp_end_time", float(bp_duration))
        bp_start = self.music_cfg.get("bp_start_time")
        if bp_start is None:
            bp_start = bp_end - 75.0   # 往回推 75 秒
            logger.info(f"bp_start_time 未設定，自動推算: {bp_end:.0f}s - 75s = {bp_start:.0f}s")

        raw_bp     = temp_dir / "bp_raw.mp4"
        encoded_bp = temp_dir / "bp_encoded.mp4"
        bp_final   = temp_dir / "bp_final.mp4"
        preset = self.out_cfg.get("ffmpeg_preset", "medium")
        crf    = self.out_cfg.get("ffmpeg_crf", 18)

        self._run([
            "ffmpeg", "-y",
            "-ss", str(bp_start), "-to", str(bp_end),
            "-i", str(source),
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            str(raw_bp),
        ], f"擷取 BP 片段 ({bp_start:.0f}s ~ {bp_end:.0f}s)")

        # 統一 1080p encode，BP 只保留原聲（不加音樂）
        self._run([
            "ffmpeg", "-y",
            *_hwaccel_input(raw_bp),
            "-vf", f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                   f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2",
            *_video_codec_args(preset, crf),
            "-c:a", "aac", "-b:a", "192k",
            str(bp_final),
        ], "Encode BP 片頭（原聲，無音樂）")
        raw_bp.unlink(missing_ok=True)

        return bp_final

    def _prepend_bp(self, bp_clip: Path, highlight: Path, output: Path) -> Path:
        """
        將 BP 片頭用 xfade fadeblack 拼接到精華影片最前面。
        取代原本的 7 秒黑色過場，改用 0.5s fadeblack 轉場。
        """
        bp_dur = self._get_duration(bp_clip)
        xd     = XFADE_DURATION
        offset = bp_dur - xd

        fc = (
            f"[0:v][1:v]xfade=transition=fadeblack:duration={xd}:offset={offset:.3f}[v];"
            f"[0:a][1:a]acrossfade=d={xd}[a]"
        )
        preset = self.out_cfg.get("ffmpeg_preset", "medium")
        crf    = str(self.out_cfg.get("ffmpeg_crf", 18))

        self._run([
            "ffmpeg", "-y",
            *_hwaccel_input(bp_clip), *_hwaccel_input(highlight),
            "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]",
            *_video_codec_args(preset, crf),
            "-c:a", "aac", "-b:a", "192k",
            str(output),
        ], "BP 片頭 xfade 拼接（0.5s fadeblack）")

        return output

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def _pick_gameplay_music(self) -> Path | None:
        music_dir_raw = self.music_cfg.get("gameplay_music_dir", "assets/music")
        music_dir = Path(music_dir_raw)
        # 相對路徑 → 以專案根目錄（modules/ 上一層）為基準轉成絕對路徑
        if not music_dir.is_absolute():
            music_dir = (Path(__file__).parent.parent / music_dir).resolve()
        exclude   = set(self.music_cfg.get("gameplay_music_exclude", []))
        bpm_min   = float(self.music_cfg.get("gameplay_bpm_min", 0))
        bpm_max   = float(self.music_cfg.get("gameplay_bpm_max", 9999))

        if not music_dir.exists():
            return None

        # 嘗試讀取 BPM catalog（由 music_library.py scan 產生）
        catalog: dict = {}
        catalog_path = music_dir / "catalog.json"
        if catalog_path.exists():
            try:
                with open(catalog_path, encoding="utf-8") as f:
                    catalog = json.load(f)
            except Exception:
                logger.warning("catalog.json 讀取失敗，忽略 BPM 篩選")

        all_files = [
            f for f in list(music_dir.glob("*.m4a")) + list(music_dir.glob("*.mp3"))
            if f.name not in exclude
        ]

        # 套用 BPM 篩選（有 catalog 且有 BPM 設定才生效）
        if catalog and (bpm_min > 0 or bpm_max < 9999):
            filtered = [
                f for f in all_files
                if f.name in catalog and bpm_min <= catalog[f.name]["bpm"] <= bpm_max
            ]
            # fallback：catalog 裡沒有符合的，退回全部（不中斷流程）
            candidates = filtered if filtered else all_files
            if not filtered:
                logger.warning(
                    f"catalog 中找不到 BPM {bpm_min:.0f}~{bpm_max:.0f} 的曲目，"
                    f"退回全部 {len(all_files)} 首"
                )
        else:
            candidates = all_files

        if not candidates:
            return None

        chosen = random.choice(candidates)
        bpm_str = ""
        if chosen.name in catalog:
            bpm_str = f"  BPM={catalog[chosen.name]['bpm']:.1f}"
        logger.info(f"選用 BGM: {chosen.name}{bpm_str}")
        return chosen

    def _probe_resolution(self, video: Path) -> tuple[int, int]:
        result = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", str(video),
        ], capture_output=True, text=True)
        for s in json.loads(result.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return s.get("width", 1920), s.get("height", 1080)
        return 1920, 1080

    def _get_duration(self, video: Path) -> float:
        result = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(video),
        ], capture_output=True, text=True)
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])

    def _cleanup(self, temp_dir: Path, output_path: Path):
        for f in temp_dir.iterdir():
            if f.resolve() != output_path.resolve():
                try:
                    f.unlink()
                except Exception:
                    pass
        try:
            temp_dir.rmdir()
        except Exception:
            pass

    def _run(self, cmd: list[str], desc: str = ""):
        logger.info(f"FFmpeg: {desc}")
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            logger.error(f"FFmpeg 錯誤（{desc}）:\n{result.stderr[-2000:]}")
            raise RuntimeError(f"FFmpeg 失敗: {desc}")
