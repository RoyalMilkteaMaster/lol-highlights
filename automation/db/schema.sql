-- ============================================================================
--  LoL 賽程爬蟲 — MySQL Schema
--  字符集統一 utf8mb4（支援表情符號／韓文／中文）
--  引擎統一 InnoDB（支援交易與外鍵）
-- ============================================================================

-- ── 1. 聯賽（leagues）────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leagues (
    league_id    TINYINT UNSIGNED PRIMARY KEY,           -- 01~99，與流水編號的 LL 對應
    code         VARCHAR(16)  NOT NULL UNIQUE,           -- 'LCK','LPL','LEC'...
    name         VARCHAR(64)  NOT NULL,                  -- 'LCK Spring 2026'
    region       VARCHAR(16),                            -- 'KR','CN','EU','INTL'
    external_id  VARCHAR(32),                            -- lolesports.com league id
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 2. 隊伍（teams）─────────────────────────────────────────────────────────
-- code 不做全域 UNIQUE，避免不同聯賽縮寫撞號（學院隊、二隊風險更高）
-- 真正唯一性由 external_ids 表負責
CREATE TABLE IF NOT EXISTS teams (
    team_id      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    code         VARCHAR(8)   NOT NULL,                  -- 'T1','GEN','BLG' (HUD 縮寫)
    name         VARCHAR(64)  NOT NULL,                  -- 'T1','Gen.G Esports' 完整名
    league_id    TINYINT UNSIGNED NOT NULL,              -- 主屬聯賽
    logo_url     VARCHAR(255),                           -- Phase B 才填
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (league_id) REFERENCES leagues(league_id),
    UNIQUE KEY uk_league_code (league_id, code),
    INDEX idx_league (league_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 3. 系列賽（series）─ BO 整體（主表）──────────────────────────────────
-- series_id 為 14 位流水：YYYYMMDDLLNNNG，末位 G=0 代表系列賽
CREATE TABLE IF NOT EXISTS series (
    series_id           BIGINT UNSIGNED PRIMARY KEY,
    match_code          VARCHAR(64) NOT NULL,            -- '20260504_LCK_003_T1vsGEN'（人類可讀，不 UNIQUE）
    league_id           TINYINT UNSIGNED NOT NULL,
    match_date          DATE NOT NULL,                   -- 在地日期
    match_time          TIME NOT NULL,                   -- 在地時間
    timezone            VARCHAR(64) NOT NULL,            -- 'Asia/Seoul','Asia/Shanghai'...
    match_datetime_utc  DATETIME NOT NULL,               -- UTC 標準（跨時區比對用）
    team_a_id           INT UNSIGNED NOT NULL,
    team_b_id           INT UNSIGNED NOT NULL,
    best_of             TINYINT NOT NULL,                -- 1/3/5
    stage               VARCHAR(32),                     -- 'Regular Season','Playoffs'
    status              ENUM('scheduled','live','completed','cancelled')
                            NOT NULL DEFAULT 'scheduled',
    winner_team_id      INT UNSIGNED,                    -- 賽前 NULL
    score_a             TINYINT,
    score_b             TINYINT,
    stream_url          VARCHAR(255),                    -- 直播連結
    vod_url             VARCHAR(255),                    -- 主 VOD
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (league_id)      REFERENCES leagues(league_id),
    FOREIGN KEY (team_a_id)      REFERENCES teams(team_id),
    FOREIGN KEY (team_b_id)      REFERENCES teams(team_id),
    FOREIGN KEY (winner_team_id) REFERENCES teams(team_id),
    INDEX idx_date         (match_date),
    INDEX idx_league_date  (league_id, match_date),
    INDEX idx_status       (status),
    INDEX idx_match_code   (match_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 4. 每局（games）─ 用戶要求每局一筆 ───────────────────────────────────
-- game_id 末位 G=1~9 代表第 N 局
CREATE TABLE IF NOT EXISTS games (
    game_id         BIGINT UNSIGNED PRIMARY KEY,
    series_id       BIGINT UNSIGNED NOT NULL,
    game_number     TINYINT NOT NULL,                    -- 1~5
    winner_team_id  INT UNSIGNED,
    vod_url         VARCHAR(255),                        -- 該局單獨 VOD
    riot_game_id    VARCHAR(64),                         -- 未來對接 Riot Match v5
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (series_id)      REFERENCES series(series_id) ON DELETE CASCADE,
    FOREIGN KEY (winner_team_id) REFERENCES teams(team_id),
    UNIQUE KEY uk_series_game (series_id, game_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 5. 外部 ID 對應（external_ids）─ 識別「同一筆」的真依據 ──────────────
-- ⚠️ Polymorphic Reference：DB 不強制 FK，必須由 Repository 層保證 entity_type
--    與 entity_id 對應正確（e.g. entity_type='team' 時 entity_id 必須是 teams.team_id）
CREATE TABLE IF NOT EXISTS external_ids (
    id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    entity_type  ENUM('league','team','series','game') NOT NULL,
    entity_id    BIGINT UNSIGNED NOT NULL,
    source       VARCHAR(32)  NOT NULL,                  -- 'lolesports','leaguepedia'
    external_id  VARCHAR(64)  NOT NULL,
    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_source_ext (entity_type, source, external_id),
    INDEX idx_entity (entity_type, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 6. 直播（broadcasts）─ Phase 49 新增 ────────────────────────────────────
-- 一個 broadcast = 一個直播實例（YouTube live / Bilibili live room）
-- 一個 broadcast 可對應多個 series（同一天直播 BO3 + BO3）→ broadcast_series 多對多
-- broadcast_id 改 AUTO_INCREMENT 避免同天主/副頻道/中斷重開撞號（ChatGPT #1）
CREATE TABLE IF NOT EXISTS broadcasts (
    broadcast_id        BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform            VARCHAR(20)  NOT NULL,                  -- 'youtube' / 'bilibili'
    external_id         VARCHAR(100) NOT NULL,                  -- YT video_id / BL room_id
    channel_id          VARCHAR(100),                           -- YT channel_id / BL room owner uid
    league_id           TINYINT UNSIGNED NOT NULL,
    league_code         VARCHAR(10)  NOT NULL,                  -- 冗餘但查詢方便
    league_timezone     VARCHAR(50)  NOT NULL,                  -- 'Asia/Seoul' / 'Asia/Shanghai'
    broadcast_date      DATE         NOT NULL,                  -- league local date（命名 / 查詢用）
    stream_url          VARCHAR(500),
    vod_url             VARCHAR(500),
    title               VARCHAR(500),
    scheduled_start_utc DATETIME,                               -- UTC（ChatGPT #3）
    actual_start_utc    DATETIME,                               -- UTC
    actual_end_utc      DATETIME,                               -- UTC
    recording_path      VARCHAR(500),                           -- 錄影檔絕對路徑
    recording_status    VARCHAR(30),                            -- 'pending'/'recording'/'done'/'failed'
    source_status       VARCHAR(30),                            -- 'upcoming'/'pending_confirm'/'live'/'ended'/'offline'/'unknown'
    confidence          VARCHAR(20),                            -- 'high'/'medium'/'low'
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

-- ── 7. 直播 ↔ 系列賽 對應（broadcast_series）─ Phase 49 新增 ────────────────
-- 多對多 join table；一個直播可對應多場 series（一天 BO3 + BO3）
CREATE TABLE IF NOT EXISTS broadcast_series (
    broadcast_id          BIGINT UNSIGNED NOT NULL,
    series_id             BIGINT UNSIGNED NOT NULL,
    series_order          TINYINT NOT NULL,                     -- 同一直播第幾場 series（1-based）
    mapping_confidence    VARCHAR(20) NOT NULL DEFAULT 'low',   -- high/medium/low（ChatGPT #11）
    mapping_source        VARCHAR(50) NOT NULL,                 -- 'title_pair+date' 等
    mapping_reason        VARCHAR(200),                         -- debug 文字
    created_at            TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (broadcast_id, series_id),
    FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE,
    FOREIGN KEY (series_id)    REFERENCES series(series_id),
    INDEX idx_series_id (series_id),
    INDEX idx_confidence (mapping_confidence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 8. Migration 紀錄表（給 init_db --migrate 用）─ Phase 49 新增 ──────────
CREATE TABLE IF NOT EXISTS migrations (
    migration_name      VARCHAR(255) PRIMARY KEY,                -- '001_add_broadcasts.sql'
    applied_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    checksum            VARCHAR(64)                              -- SHA-256（hex）
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ⚠️ 已 deprecated 的欄位（Phase 49 之後改寫到 broadcasts.stream_url / broadcasts.vod_url）：
--    series.stream_url / series.vod_url / games.vod_url
--   不刪欄位（向下相容），但新流程不再寫入。
