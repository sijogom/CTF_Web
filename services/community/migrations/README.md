# Community DB 마이그레이션

`alembic upgrade head`는 Community 서비스보다 먼저 실행해야 한다. 앱 startup은 schema를
생성하거나 ALTER하지 않는다.

## 안전 규칙

- 실행 전에 PostgreSQL backup과 schema inventory를 남긴다.
- `20260813_000`은 빈 DB를 생성하거나, 모든 legacy Community table이 이미 있는 DB의
  계약만 검사한다. 일부 table만 있는 DB는 자동 보정하지 않고 실패한다.
- `20260813_001`은 과거 startup이 처리하던 `posts` 확장 컬럼을 없을 때만 추가한다.
- `20260813_002`는 moderation ledger가 없을 때만 생성한다. 이미 일부만 존재하거나
  column/constraint/index 계약이 다르면 실패한다.
- moderation event가 기록된 뒤에는 ledger downgrade로 table을 drop하지 않는다. 복구는
  승인된 backup restore로만 수행한다.

실제 PostgreSQL 마이그레이션은 대상 DB의 백업·스키마 점검 후 별도로 검증합니다.

환경값과 DB 접속 비밀은 저장소 밖에서 주입합니다. 테스트용 DB와 운영 DB를 구분합니다.
