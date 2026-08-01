#!/bin/bash
set -euo pipefail

case "${WARRANTY_FLAG:-}" in
  ""|*"'"*)
    echo "WARRANTY_FLAG must be a non-empty value without single quotes" >&2
    exit 1
    ;;
esac

mysql --protocol=socket \
  -uroot \
  -p"${MYSQL_ROOT_PASSWORD}" \
  "${MYSQL_DATABASE}" \
  --execute="INSERT INTO challenge_settings (setting_name, setting_value) VALUES ('verification_token', '${WARRANTY_FLAG}') ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value);"
