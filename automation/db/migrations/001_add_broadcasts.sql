-- ============================================================================
--  Migration 001：新增 broadcasts + broadcast_series + migrations 三表
--  Phase 49（2026-05-06）— URL 取得 / 命名規則設計
--
--  特性：
--    - CREATE TABLE IF NOT EXISTS：重複跑不爆炸
--    - 不動既有 leagues / teams / series / games / external_ids 結構
--    - series.stream_url / series.vod_url / games.vod_url 保留（deprecated）
-- ============================================================================

-- broadcasts：直播實例（一個 broadcast = 一個 YT live / BL room 直播）
CREATE TABLE IF NOT EXISTS broadcasts (
    broadcast_id        BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform            VARCHAR(20)  NOT NULL,
    external_id         VARCHAR(100) NOT NULL,
    channel_id          VARCHAR(100),
    league_id           TINYINT UNSIGNED NOT NULL,
    league_code         VARCHAR(10)  NOT NULL,
    league_timezone     VARCHAR(50)  NOT NULL,
    broadcast_date      DATE         NOT NULL,
    stream_url          VARCHAR(500),
    vod_url             VARCHAR(500),
    title               VARCHAR(500),
    scheduled_start_utc DATETIME,
    actual_start_utc    DATETIME,
    actual_end_utc      DATETIME,
    recording_path      VARCHAR(500),
    recording_status    VARCHAR(30),
    source_status       VARCHAR(30),
    confidence          VARCHAR(20),
    last_checked_at     DATETIME,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_platform_external_date (platform, external_id, broadcast_date),
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    INDEX idx_date_league (broadcast_date, league_id),
    INDEX idx_recording_status (recording_status),
    INDEX idx_scheduled (scheduled_start_utc),
    INDEX idx_source_status (source_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- broadcast_series：直播 ↔ 系列賽（多對多 join，含 mapping confidence）
CREATE TABLE IF NOT EXISTS broadcast_series (
    broadcast_id          BIGINT UNSIGNED NOT NULL,
    series_id             BIGINT UNSIGNED NOT NULL,
    series_order          TINYINT NOT NULL,
    mapping_confidence    VARCHAR(20) NOT NULL DEFAULT 'low',
    mapping_source        VARCHAR(50) NOT NULL,
    mapping_reason        VARCHAR(200),
    created_at            TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (broadcast_id, series_id),
    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE,
    FOREIGN KEY (series_id)    REFERENCES series(series_id),
    INDEX idx_series_id (series_id),
    INDEX idx_confidence (mapping_confidence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- migrations：給 init_db.py --migrate 用，記錄已跑過的 migration
CREATE TABLE IF NOT EXISTS migrations (
    migration_name      VARCHAR(255) PRIMARY KEY,
    applied_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    checksum            VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
