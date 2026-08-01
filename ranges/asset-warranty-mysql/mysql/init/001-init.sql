CREATE DATABASE IF NOT EXISTS asset_warranty
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE asset_warranty;

CREATE TABLE IF NOT EXISTS warranty_records (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    asset_no VARCHAR(64) NOT NULL,
    department VARCHAR(64) NOT NULL,
    device_name VARCHAR(128) NOT NULL,
    warranty_until DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_asset_department (asset_no, department)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS challenge_settings (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    setting_name VARCHAR(128) NOT NULL,
    setting_value VARCHAR(512) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_setting_name (setting_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO warranty_records (
    asset_no, department, device_name, warranty_until, status
) VALUES (
    'PC-2026-013', 'OPS', 'Operations Workstation', '2027-12-31', 'ACTIVE'
)
ON DUPLICATE KEY UPDATE
    device_name = VALUES(device_name),
    warranty_until = VALUES(warranty_until),
    status = VALUES(status);

REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'warranty_app'@'%';
GRANT SELECT ON asset_warranty.* TO 'warranty_app'@'%';
FLUSH PRIVILEGES;
