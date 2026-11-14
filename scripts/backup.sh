#!/bin/bash
# DB 자동 백업 스크립트
# 크론잡: 0 3 * * * /path/to/backup.sh

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/backups}"
DATABASES=("ctfd_db" "learn_db" "community_db" "platform_db")
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_USER="${POSTGRES_USER:-ctfplatform}"

# pg_dump 인증을 위한 비밀번호 설정 (필수)
# 대안: ~/.pgpass 파일에 hostname:port:database:username:password 형식으로 저장 가능
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD 환경변수가 설정되지 않았습니다}"

mkdir -p "$BACKUP_DIR"

echo "[$TIMESTAMP] 백업 시작..."

for DB in "${DATABASES[@]}"; do
  echo "  -> $DB 백업 중..."
  pg_dump -h "$PG_HOST" -U "$PG_USER" "$DB" | gzip > "${BACKUP_DIR}/${DB}_${TIMESTAMP}.sql.gz"
  echo "  -> $DB 완료: ${DB}_${TIMESTAMP}.sql.gz"
done

# MinIO에 업로드 (mc 클라이언트가 설치되어 있을 때만)
if command -v mc &> /dev/null; then
  mc cp "${BACKUP_DIR}"/*_${TIMESTAMP}.sql.gz minio/backups/ 2>/dev/null || echo "  [WARN] MinIO 업로드 실패"
fi

# 7일 이전 로컬 백업 삭제
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +7 -delete 2>/dev/null || true

echo "[$TIMESTAMP] 백업 완료!"
