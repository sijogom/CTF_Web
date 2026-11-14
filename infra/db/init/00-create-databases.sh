#!/bin/bash
# 서비스별 논리 DB + 전용 유저 생성 (비밀번호는 환경변수에서 주입)
# 보안: psql -v 옵션으로 변수를 전달하여 SQL 인젝션 방지
# 보안: 비밀번호 내 작은따옴표를 이스케이프 처리 ('' 치환)
set -e

# 비밀번호 이스케이프 — 작은따옴표(')를 PostgreSQL 이스케이프('')로 치환
escape_pw() {
    printf '%s' "${1//\'/\'\'}"
}

AUTH_PW_SAFE=$(escape_pw "${AUTH_DB_PASSWORD:-changeme_auth}")
LEARN_PW_SAFE=$(escape_pw "${LEARN_DB_PASSWORD:-changeme_learn}")
COMMUNITY_PW_SAFE=$(escape_pw "${COMMUNITY_DB_PASSWORD:-changeme_community}")

# 1단계: DB 생성 (변수 바인딩 불필요 — 고정 이름)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE ctfd_db;
    CREATE DATABASE learn_db;
    CREATE DATABASE community_db;
    CREATE DATABASE platform_db;
EOSQL

# 2단계: 유저 생성 + 권한 부여 (psql -v로 비밀번호를 안전하게 전달)
psql -v auth_pass="'${AUTH_PW_SAFE}'" \
     -v learn_pass="'${LEARN_PW_SAFE}'" \
     -v community_pass="'${COMMUNITY_PW_SAFE}'" \
     -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- 서비스별 전용 유저 (최소 권한 원칙, 비밀번호는 psql 변수로 전달)
    CREATE USER auth_svc WITH PASSWORD :auth_pass;
    CREATE USER learn_svc WITH PASSWORD :learn_pass;
    CREATE USER community_svc WITH PASSWORD :community_pass;

    -- DB 연결 권한 (CONNECT만 부여, ALL PRIVILEGES 대신 최소 권한)
    GRANT CONNECT ON DATABASE platform_db TO auth_svc;
    GRANT CONNECT ON DATABASE learn_db TO learn_svc;
    GRANT CONNECT ON DATABASE community_db TO community_svc;
EOSQL

# 3단계: 각 DB에 접속하여 스키마 권한 + DEFAULT PRIVILEGES 설정
# 테이블이 아직 없으므로 DEFAULT PRIVILEGES로 향후 생성될 테이블에 권한 자동 부여
for db_user in "platform_db:auth_svc" "learn_db:learn_svc" "community_db:community_svc"; do
    db="${db_user%%:*}"
    user="${db_user##*:}"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<-EOSQL
        -- public 스키마 사용 권한
        GRANT USAGE, CREATE ON SCHEMA public TO ${user};

        -- 향후 생성될 테이블에 DML 권한 자동 부여
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${user};

        -- 향후 생성될 시퀀스에 사용 권한 자동 부여 (SERIAL/BIGSERIAL 컬럼용)
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO ${user};
EOSQL
done
